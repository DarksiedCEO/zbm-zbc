package main

// Fakes for the Revenue Recovery fix-wave tests (Oct 6 2026). They follow the
// real contracts closely enough that the same HTTP-level tests run against
// the pre-fix orchestrator (integration 5d49ee9) and fail there for the
// reason the sweep found, not because of a fake:
//   - fakeDetection behaves like detection-py: lists of more than 1,000 items
//     and JSON null lists are 422; correlation keys by (client, entity_type,
//     entity_id) and needs two distinct agents.
//   - memLedger behaves like ledger-rust: POST /ledger/events is idempotent on
//     event_id (201 new, 200 identical, 409 different), POST /ledger/append
//     appends, GET /ledger/entries returns everything, and writes can be made
//     to fail before or after they commit.

import (
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync"
	"testing"
)

const tenant = "fixture-pool"

// findingID is detection-py's zbm_schema.compute_finding_id, written out
// independently of the orchestrator's own implementation.
func findingID(client, agent, etype, eid, period string) string {
	sum := sha256.Sum256([]byte(strings.Join([]string{"rrf1", client, agent, etype, eid, period}, "\n")))
	return "rrf1-" + hex.EncodeToString(sum[:])[:40]
}

var agentCategory = map[string]string{
	"affiliate-coupon-extension-v1":  "affiliate_coupon_extension",
	"discount-misuse-v1":             "discount_misuse",
	"abandoned-cart-coverage-v1":     "abandoned_cart_coverage",
	"renewal-never-triggered-v1":     "renewal_never_triggered",
	"server-side-attribution-v1":     "server_side_attribution_gap",
	"cross-channel-attribution-v1":   "cross_channel_misattribution_risk",
	"platform-integration-v1":        "platform_integration_gap",
	"contract-pricing-term-drift-v1": "contract_pricing_term_drift",
}

var pathAgent = map[string]string{
	"/agents/affiliate-coupon-extension/detect":  "affiliate-coupon-extension-v1",
	"/agents/discount-misuse/detect":             "discount-misuse-v1",
	"/agents/abandoned-cart-coverage/detect":     "abandoned-cart-coverage-v1",
	"/agents/renewal-never-triggered/detect":     "renewal-never-triggered-v1",
	"/agents/server-side-attribution/detect":     "server-side-attribution-v1",
	"/agents/cross-channel-attribution/detect":   "cross-channel-attribution-v1",
	"/agents/platform-integration/detect":        "platform-integration-v1",
	"/agents/contract-pricing-term-drift/detect": "contract-pricing-term-drift-v1",
}

// mkFinding is a finding exactly as detection-py serializes one. amount ""
// means no recoverable value (evidence UNKNOWN).
func mkFinding(client, agent, etype, eid, amount string) map[string]any {
	f := map[string]any{
		"finding_id": findingID(client, agent, etype, eid, ""), "client_id": client, "agent_id": agent,
		"leak_category": agentCategory[agent], "entity_type": etype, "entity_id": eid, "period_label": nil,
		"customer_id": "c1", "cause_certainty": "named", "cause_description": "test finding",
		"recoverable_value": nil, "evidence_class": "UNKNOWN", "methodology_id": "test_method",
		"methodology": "test methodology", "detected_at": "2026-10-06T00:00:00Z",
	}
	if amount != "" {
		f["recoverable_value"] = map[string]any{"amount_usd": amount, "classification": "observed", "confidence": "high"}
		f["evidence_class"] = "OBSERVED"
	}
	return f
}

type fakeDetection struct {
	mu       sync.Mutex
	reqs     []string
	fixtures map[string]string // "/fixtures/..." -> JSON list; default []
	// rules: agent id -> findings for one batch of items (the request's
	// list, decoded) for the given client. Default: no findings.
	rules map[string]func(client string, items []map[string]any) []map[string]any
	// batch sizes seen per path, and correlation request sizes
	sizes map[string][]int
	// started, if set, is closed (once) on the first detect request; the
	// request then waits for release.
	started chan struct{}
	release chan struct{}
	once    sync.Once
}

func newFakeDetection() *fakeDetection {
	return &fakeDetection{fixtures: map[string]string{}, rules: map[string]func(string, []map[string]any) []map[string]any{},
		sizes: map[string][]int{}}
}

func (d *fakeDetection) requests() []string {
	d.mu.Lock()
	defer d.mu.Unlock()
	return append([]string(nil), d.reqs...)
}

func (d *fakeDetection) batchSizes(path string) []int {
	d.mu.Lock()
	defer d.mu.Unlock()
	return append([]int(nil), d.sizes[path]...)
}

func unprocessable(w http.ResponseWriter, msg string) {
	w.WriteHeader(http.StatusUnprocessableEntity)
	_, _ = fmt.Fprintf(w, `{"detail":[{"type":"fake","msg":%q}]}`, msg)
}

func (d *fakeDetection) server(t *testing.T) *httptest.Server {
	t.Helper()
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		d.mu.Lock()
		d.reqs = append(d.reqs, r.Method+" "+r.URL.Path)
		d.mu.Unlock()
		w.Header().Set("Content-Type", "application/json")
		if strings.HasPrefix(r.URL.Path, "/fixtures/") {
			body, ok := d.fixtures[r.URL.Path]
			if !ok {
				body = "[]"
			}
			_, _ = w.Write([]byte(body))
			return
		}
		raw, _ := io.ReadAll(r.Body)
		var req map[string]json.RawMessage
		if err := json.Unmarshal(raw, &req); err != nil {
			unprocessable(w, "not a JSON object")
			return
		}
		if r.URL.Path == "/correlation/overlaps" {
			d.correlate(w, req)
			return
		}
		agent, ok := pathAgent[r.URL.Path]
		if !ok {
			w.WriteHeader(http.StatusNotFound)
			return
		}
		if d.started != nil {
			d.once.Do(func() { close(d.started) })
			<-d.release
		}
		client := tenant // pre-fix callers send no client_id; the fake does not judge that
		if c, ok := req["client_id"]; ok {
			_ = json.Unmarshal(c, &client)
		}
		var items []map[string]any
		for k, v := range req {
			if k == "client_id" || k == "as_of" {
				continue
			}
			if string(v) == "null" {
				unprocessable(w, k+": Input should be a valid list")
				return
			}
			if err := json.Unmarshal(v, &items); err != nil {
				unprocessable(w, k+": not a list")
				return
			}
		}
		if len(items) > 1000 {
			unprocessable(w, fmt.Sprintf("List should have at most 1000 items after validation, not %d", len(items)))
			return
		}
		d.mu.Lock()
		d.sizes[r.URL.Path] = append(d.sizes[r.URL.Path], len(items))
		rule := d.rules[agent]
		d.mu.Unlock()
		findings := []map[string]any{}
		if rule != nil {
			findings = append(findings, rule(client, items)...)
		}
		_ = json.NewEncoder(w).Encode(map[string]any{"findings": findings})
	}))
	t.Cleanup(srv.Close)
	return srv
}

func (d *fakeDetection) correlate(w http.ResponseWriter, req map[string]json.RawMessage) {
	raw := req["findings"]
	if string(raw) == "null" || raw == nil {
		unprocessable(w, "findings: Input should be a valid list")
		return
	}
	var fs []map[string]any
	if err := json.Unmarshal(raw, &fs); err != nil {
		unprocessable(w, "findings: not a list")
		return
	}
	if len(fs) > 1000 {
		unprocessable(w, fmt.Sprintf("List should have at most 1000 items after validation, not %d", len(fs)))
		return
	}
	d.mu.Lock()
	d.sizes["/correlation/overlaps"] = append(d.sizes["/correlation/overlaps"], len(fs))
	d.mu.Unlock()
	groups := map[string][]map[string]any{}
	agents := map[string]map[string]bool{}
	for _, f := range fs {
		k := fmt.Sprintf("%v|%v|%v", f["client_id"], f["entity_type"], f["entity_id"])
		groups[k] = append(groups[k], f)
		if agents[k] == nil {
			agents[k] = map[string]bool{}
		}
		agents[k][fmt.Sprint(f["agent_id"])] = true
	}
	out := map[string][]map[string]any{}
	for k, g := range groups {
		if len(agents[k]) > 1 {
			out[k] = g
		}
	}
	_ = json.NewEncoder(w).Encode(out)
}

// orders is a fixture list of n orders o0..o(n-1).
func orders(n int) string {
	items := make([]string, n)
	for i := range items {
		items[i] = fmt.Sprintf(`{"order_id":"o%d","customer_id":"c1"}`, i)
	}
	return "[" + strings.Join(items, ",") + "]"
}

// everyOrder returns one finding per order item for agent (amount "" = none).
func everyOrder(agent, amount string) func(string, []map[string]any) []map[string]any {
	return func(client string, items []map[string]any) []map[string]any {
		out := []map[string]any{}
		for _, it := range items {
			out = append(out, mkFinding(client, agent, "order", fmt.Sprint(it["order_id"]), amount))
		}
		return out
	}
}

// ---------------------------------------------------------------------------

type memEntry map[string]any

type memLedger struct {
	mu      sync.Mutex
	entries []memEntry
	byEvent map[string]int // event_id -> index
	writes  int            // write attempts (append + events)
	// failAt: the write attempt numbers (1-based) that fail with 500.
	// commitThenFail: those attempts commit first and THEN answer 500.
	failAt         map[int]bool
	failAlways     bool // every write from failFrom on fails, uncommitted
	failFrom       int
	commitThenFail bool
}

func newMemLedger() *memLedger { return &memLedger{byEvent: map[string]int{}, failAt: map[int]bool{}} }

func (l *memLedger) snapshot() []memEntry {
	l.mu.Lock()
	defer l.mu.Unlock()
	return append([]memEntry(nil), l.entries...)
}

func (l *memLedger) push(e memEntry) int {
	seq := len(l.entries)
	prev := "genesis"
	if seq > 0 {
		prev = l.entries[seq-1]["hash"].(string)
	}
	e["seq"], e["prev_hash"], e["recorded_at"] = seq, prev, "2026-10-06T00:00:00Z"
	sum := sha256.Sum256([]byte(fmt.Sprintf("%v", e)))
	e["hash"] = hex.EncodeToString(sum[:])
	l.entries = append(l.entries, e)
	return seq
}

func (l *memLedger) server(t *testing.T) *httptest.Server {
	t.Helper()
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		l.mu.Lock()
		defer l.mu.Unlock()
		w.Header().Set("Content-Type", "application/json")
		switch {
		case r.Method == http.MethodGet && r.URL.Path == "/ledger/verify":
			_, _ = fmt.Fprintf(w, `{"valid":true,"entries":%d}`, len(l.entries))
		case r.Method == http.MethodGet && r.URL.Path == "/ledger/entries":
			_ = json.NewEncoder(w).Encode(l.entries)
		case r.Method == http.MethodPost && (r.URL.Path == "/ledger/events" || r.URL.Path == "/ledger/append"):
			l.writes++
			n := l.writes
			fail := l.failAt[n] || (l.failAlways && n >= l.failFrom)
			if fail && !l.commitThenFail {
				w.WriteHeader(http.StatusInternalServerError)
				_, _ = w.Write([]byte(`{"error":"injected: not committed"}`))
				return
			}
			var body map[string]any
			raw, _ := io.ReadAll(r.Body)
			if err := json.Unmarshal(raw, &body); err != nil {
				w.WriteHeader(http.StatusBadRequest)
				return
			}
			code := http.StatusCreated
			var seq int
			if r.URL.Path == "/ledger/events" {
				id := fmt.Sprint(body["event_id"])
				if i, ok := l.byEvent[id]; ok {
					prior := l.entries[i]
					for _, k := range []string{"department", "event_type", "actor", "subject_id", "payload_sha256", "summary"} {
						if prior[k] != body[k] {
							w.WriteHeader(http.StatusConflict)
							_, _ = w.Write([]byte(`{"error":"conflict"}`))
							return
						}
					}
					code, seq = http.StatusOK, i
				} else {
					body["kind"] = "event"
					seq = l.push(body)
					l.byEvent[id] = seq
				}
			} else {
				body["kind"] = "finding"
				seq = l.push(body)
				code = http.StatusOK
			}
			if fail { // committed, but the caller is told it failed
				w.WriteHeader(http.StatusInternalServerError)
				_, _ = w.Write([]byte(`{"error":"injected after commit"}`))
				return
			}
			w.WriteHeader(code)
			_, _ = fmt.Fprintf(w, `{"seq":%d,"hash":"h","prev_hash":"p"}`, seq)
		default:
			w.WriteHeader(http.StatusNotFound)
		}
	}))
	t.Cleanup(srv.Close)
	return srv
}

// findingEntries counts the ledger entries that record a finding, of either
// shape: a pre-fix "kind":"finding" append or a fix-wave rr_finding event.
func (l *memLedger) findingEntries() int {
	n := 0
	for _, e := range l.snapshot() {
		if e["kind"] == "finding" || e["event_type"] == "rr_finding" {
			n++
		}
	}
	return n
}
