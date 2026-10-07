package main

// Revenue Recovery fix wave (Oct 6 2026): regression tests for the backend
// bug sweep's orchestrator findings, through the real HTTP routes. Each one
// reproduces a sweep probe (scratchpad/sweep-E: live_clean_store.py,
// probe_nil_findings.py, live_rr.py L1-L5, probe_detection.py P8-P10) and
// fails on integration 5d49ee9.

import (
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/DarksiedCEO/zbm-zbc/services/orchestrator-go/internal/orchestrator"
)

func fixWaveServer(t *testing.T, det *fakeDetection, led *memLedger) *httptest.Server {
	t.Helper()
	orch := orchestrator.New(det.server(t).URL, "d", led.server(t).URL, "l")
	srv := httptest.NewServer(newMux(orch, routeTestToken))
	t.Cleanup(srv.Close)
	return srv
}

type scanBody struct {
	ScanID            string                       `json:"scan_id"`
	ClientID          string                       `json:"client_id"`
	TenantDefaulted   bool                         `json:"tenant_defaulted"`
	Findings          json.RawMessage              `json:"findings"`
	OverlappingClaims map[string][]json.RawMessage `json:"overlapping_claims"`
}

type findingsBody struct {
	Findings []struct {
		FindingID     string  `json:"finding_id"`
		AmountUSD     *string `json:"amount_usd"`
		TimesRecorded int     `json:"times_recorded"`
	} `json:"findings"`
	OverlappingClaims map[string]json.RawMessage `json:"overlapping_claims"`
	ExcludedScans     []struct {
		Reason string `json:"reason"`
	} `json:"excluded_scans"`
}

func scan(t *testing.T, srv *httptest.Server, query string) (int, scanBody, string) {
	t.Helper()
	code, body := do(t, http.MethodPost, srv.URL+"/revenue-recovery/scan"+query, routeTestToken)
	var sb scanBody
	_ = json.Unmarshal([]byte(body), &sb)
	return code, sb, body
}

func recorded(t *testing.T, srv *httptest.Server) findingsBody {
	t.Helper()
	code, body := do(t, http.MethodGet, srv.URL+"/revenue-recovery/findings", routeTestToken)
	if code != http.StatusOK {
		t.Fatalf("GET findings = %d: %s", code, body)
	}
	var fb findingsBody
	if err := json.Unmarshal([]byte(body), &fb); err != nil {
		t.Fatalf("GET findings body: %v: %s", err, body)
	}
	return fb
}

// A store with two leaks on one order (two agents) and one elsewhere.
func twoLeakDetection() *fakeDetection {
	det := newFakeDetection()
	det.fixtures["/fixtures/orders"] = orders(3)
	det.rules["discount-misuse-v1"] = func(c string, items []map[string]any) []map[string]any {
		out := []map[string]any{}
		for _, it := range items {
			if it["order_id"] == "o0" || it["order_id"] == "o1" {
				out = append(out, mkFinding(c, "discount-misuse-v1", "order", it["order_id"].(string), "4.50"))
			}
		}
		return out
	}
	det.rules["abandoned-cart-coverage-v1"] = func(c string, items []map[string]any) []map[string]any {
		out := []map[string]any{}
		for _, it := range items {
			if it["order_id"] == "o0" {
				out = append(out, mkFinding(c, "abandoned-cart-coverage-v1", "order", "o0", ""))
			}
		}
		return out
	}
	return det
}

// E-1 (sweep live_clean_store.py, probe_nil_findings.py): a store with no
// leaks made every scan fail with 502 — the orchestrator sent
// {"findings":null} to /correlation/overlaps and detection-py refused it.
func TestE1_CleanStoreScanIs200WithZeroFindings(t *testing.T) {
	det, led := newFakeDetection(), newMemLedger()
	srv := fixWaveServer(t, det, led)
	code, sb, body := scan(t, srv, "")
	if code != http.StatusOK {
		t.Fatalf("clean-store scan = %d, want 200: %s", code, body)
	}
	if string(sb.Findings) != "[]" || !strings.Contains(body, `"overlapping_claims":{}`) {
		t.Errorf("a clean store is findings [] and no overlaps, never null: %s", body)
	}
	for _, r := range det.requests() {
		if strings.HasSuffix(r, "/correlation/overlaps") {
			t.Errorf("correlation called with nothing to correlate")
		}
	}
	if fb := recorded(t, srv); len(fb.Findings) != 0 || len(fb.ExcludedScans) != 0 {
		t.Errorf("clean scan recorded as %+v", fb)
	}
}

// E-2 (sweep live_rr.py L1): the ledger fails on the 3rd write. The scan
// fails — and none of its findings may count: the caller was told it FAILED.
func TestE2_LedgerFailureMidScanLeavesNothingCounted(t *testing.T) {
	det, led := twoLeakDetection(), newMemLedger()
	led.failAlways, led.failFrom = true, 3
	srv := fixWaveServer(t, det, led)
	if code, _, body := scan(t, srv, ""); code != http.StatusBadGateway {
		t.Fatalf("scan with a failing ledger = %d, want 502: %s", code, body)
	}
	if led.findingEntries() == 0 {
		t.Fatal("test setup: the failure should come after some findings were written")
	}
	fb := recorded(t, srv)
	if len(fb.Findings) != 0 {
		t.Errorf("a failed scan's findings are counted: %+v", fb.Findings)
	}
	if len(fb.ExcludedScans) != 1 {
		t.Errorf("the failed scan should be listed as excluded: %+v", fb.ExcludedScans)
	}
}

// E-2 (sweep live_rr.py L2): the ledger COMMITS the 3rd write, then the
// answer is lost (500). The retry must not record that finding twice, and
// the scan completes.
func TestE2_CommittedWriteWithALostAnswerIsNotDuplicated(t *testing.T) {
	det, led := twoLeakDetection(), newMemLedger()
	led.failAt[3], led.commitThenFail = true, true
	srv := fixWaveServer(t, det, led)
	code, _, body := scan(t, srv, "")
	if code != http.StatusOK {
		t.Fatalf("scan = %d, want 200 after an idempotent retry: %s", code, body)
	}
	if n := led.findingEntries(); n != 3 {
		t.Errorf("ledger holds %d finding entries for 3 findings", n)
	}
	for _, f := range recorded(t, srv).Findings {
		if f.TimesRecorded != 1 {
			t.Errorf("finding %s recorded %d times by one scan", f.FindingID, f.TimesRecorded)
		}
	}
}

// E-2 (sweep live_rr.py L4): five concurrent scans each wrote every finding.
// One scan runs at a time; a concurrent request is 409 and runs nothing.
func TestE2_ConcurrentScanIs409AndRunsNothing(t *testing.T) {
	det, led := twoLeakDetection(), newMemLedger()
	det.started, det.release = make(chan struct{}), make(chan struct{})
	srv := fixWaveServer(t, det, led)

	var wg sync.WaitGroup
	var firstCode int
	wg.Add(1)
	go func() {
		defer wg.Done()
		firstCode, _, _ = scan(t, srv, "")
	}()
	<-det.started

	c := &http.Client{Timeout: 5 * time.Second}
	req, _ := http.NewRequest(http.MethodPost, srv.URL+"/revenue-recovery/scan", nil)
	req.Header.Set("Authorization", "Bearer "+routeTestToken)
	resp, err := c.Do(req)
	close(det.release)
	if err != nil {
		wg.Wait()
		t.Fatalf("second scan did not answer while the first ran: %v", err)
	}
	resp.Body.Close()
	wg.Wait()
	if resp.StatusCode != http.StatusConflict || resp.Header.Get("Retry-After") == "" {
		t.Errorf("concurrent scan = %d (Retry-After %q), want 409 with Retry-After", resp.StatusCode, resp.Header.Get("Retry-After"))
	}
	if firstCode != http.StatusOK {
		t.Errorf("first scan = %d", firstCode)
	}
	if n := led.findingEntries(); n != 3 {
		t.Errorf("ledger holds %d finding entries, want the first scan's 3", n)
	}
}

// E-2: a caller retrying a whole scan gets a second, separate scan; the
// findings view still shows each finding once.
func TestE2_RepeatedScansShowEachFindingOnce(t *testing.T) {
	det, led := twoLeakDetection(), newMemLedger()
	srv := fixWaveServer(t, det, led)
	_, a, _ := scan(t, srv, "")
	_, b, _ := scan(t, srv, "")
	if a.ScanID == "" || a.ScanID == b.ScanID {
		t.Fatalf("every scan needs its own id: %q %q", a.ScanID, b.ScanID)
	}
	fb := recorded(t, srv)
	if len(fb.Findings) != 3 {
		t.Fatalf("want 3 distinct findings, got %d", len(fb.Findings))
	}
	for _, f := range fb.Findings {
		if f.TimesRecorded != 2 {
			t.Errorf("%s recorded %d times by 2 completed scans", f.FindingID, f.TimesRecorded)
		}
	}
	if len(fb.OverlappingClaims) != 1 {
		t.Errorf("want one overlapping entity (o0), got %v", fb.OverlappingClaims)
	}
}

// E-3: scans have a tenant. Only the fixture tenant has a data source, and a
// defaulted tenant is recorded as defaulted.
func TestE3_ScanTenantParameter(t *testing.T) {
	det, led := twoLeakDetection(), newMemLedger()
	srv := fixWaveServer(t, det, led)
	if code, _, body := scan(t, srv, "?client_id=acme-store"); code != http.StatusUnprocessableEntity {
		t.Errorf("a tenant with no data source = %d, want 422: %s", code, body)
	}
	if code, _, body := scan(t, srv, "?client_id=bad%20id"); code != http.StatusBadRequest {
		t.Errorf("an invalid client_id = %d, want 400: %s", code, body)
	}
	if code, _, body := scan(t, srv, "?as_of=2026-10-01T00:00:00"); code != http.StatusBadRequest {
		t.Errorf("a naive as_of = %d, want 400: %s", code, body)
	}
	if n := len(det.requests()); n != 0 {
		t.Errorf("refused scans reached detection-py (%d calls)", n)
	}
	code, sb, body := scan(t, srv, "")
	if code != 200 || sb.ClientID != tenant || !sb.TenantDefaulted {
		t.Errorf("default scan: %d %s", code, body)
	}
	code, sb, body = scan(t, srv, "?client_id=fixture-pool&as_of=2026-10-01T00:00:00Z")
	if code != 200 || sb.ClientID != tenant || sb.TenantDefaulted || !strings.Contains(body, `"as_of":"2026-10-01T00:00:00Z"`) {
		t.Errorf("explicit fixture tenant: %d %s", code, body)
	}
}

// E-3: a finding for another client, or with an id that is not the derived
// hash, fails the scan before anything is written.
func TestE3_ForeignOrForgedFindingFailsTheScanWithNothingWritten(t *testing.T) {
	for name, mutate := range map[string]func(map[string]any){
		"other tenant": func(f map[string]any) {
			f["client_id"] = "other-client"
			f["finding_id"] = findingID("other-client", "discount-misuse-v1", "order", "o0", "")
		},
		"forged id":   func(f map[string]any) { f["finding_id"] = "disc-o0" },
		"wrong agent": func(f map[string]any) { f["agent_id"] = "abandoned-cart-coverage-v1" },
	} {
		det, led := newFakeDetection(), newMemLedger()
		det.fixtures["/fixtures/orders"] = orders(1)
		det.rules["discount-misuse-v1"] = func(c string, items []map[string]any) []map[string]any {
			f := mkFinding(c, "discount-misuse-v1", "order", "o0", "1.00")
			mutate(f)
			return []map[string]any{f}
		}
		srv := fixWaveServer(t, det, led)
		if code, _, body := scan(t, srv, ""); code != http.StatusBadGateway {
			t.Errorf("%s: scan = %d, want 502: %s", name, code, body)
		}
		if n := len(led.snapshot()); n != 0 {
			t.Errorf("%s: %d ledger entries written for a refused finding", name, n)
		}
	}
}

// E-10 (sweep probe P8): the same finding twice from one agent is one
// finding, never a same-agent "overlap".
func TestE10_DuplicateFindingIsRecordedOnceAndIsNoOverlap(t *testing.T) {
	det, led := newFakeDetection(), newMemLedger()
	det.fixtures["/fixtures/orders"] = orders(1)
	det.rules["abandoned-cart-coverage-v1"] = func(c string, _ []map[string]any) []map[string]any {
		f := mkFinding(c, "abandoned-cart-coverage-v1", "order", "o0", "")
		return []map[string]any{f, f}
	}
	srv := fixWaveServer(t, det, led)
	code, sb, body := scan(t, srv, "")
	if code != http.StatusOK || len(sb.OverlappingClaims) != 0 {
		t.Fatalf("scan = %d: %s", code, body)
	}
	if n := led.findingEntries(); n != 1 {
		t.Errorf("one finding recorded %d times", n)
	}
}

// E-6 (sweep probes P9, P10): 1,001 orders went to one detect call (422) and
// 1,200+ findings to one correlation call (422), failing the whole scan.
func TestE6_1001OrdersAndTheirFindingsAreBatched(t *testing.T) {
	det, led := newFakeDetection(), newMemLedger()
	det.fixtures["/fixtures/orders"] = orders(1001)
	det.rules["abandoned-cart-coverage-v1"] = everyOrder("abandoned-cart-coverage-v1", "")
	det.rules["discount-misuse-v1"] = everyOrder("discount-misuse-v1", "1.00")
	srv := fixWaveServer(t, det, led)
	code, sb, body := scan(t, srv, "")
	if code != http.StatusOK {
		t.Fatalf("scan = %d: %.300s", code, body)
	}
	if len(sb.OverlappingClaims) != 1001 {
		t.Errorf("want 1001 overlapping orders, got %d", len(sb.OverlappingClaims))
	}
	total := 0
	for _, n := range det.batchSizes("/agents/discount-misuse/detect") {
		if n > 1000 {
			t.Errorf("detect batch of %d orders", n)
		}
		total += n
	}
	if total != 1001 {
		t.Errorf("detect batches covered %d of 1001 orders", total)
	}
	sum := 0
	for _, n := range det.batchSizes("/correlation/overlaps") {
		if n > 1000 || n%2 != 0 {
			t.Errorf("correlation batch of %d findings (over the cap, or an entity split across batches)", n)
		}
		sum += n
	}
	if sum != 2002 {
		t.Errorf("correlation saw %d of 2002 findings", sum)
	}
}

func TestE6_1200FindingsCorrelateInBatches(t *testing.T) {
	det, led := newFakeDetection(), newMemLedger()
	det.fixtures["/fixtures/orders"] = orders(600)
	det.rules["abandoned-cart-coverage-v1"] = everyOrder("abandoned-cart-coverage-v1", "")
	det.rules["discount-misuse-v1"] = everyOrder("discount-misuse-v1", "1.00")
	srv := fixWaveServer(t, det, led)
	code, sb, body := scan(t, srv, "")
	if code != http.StatusOK || len(sb.OverlappingClaims) != 600 {
		t.Fatalf("scan = %d with %d overlaps: %.300s", code, len(sb.OverlappingClaims), body)
	}
	if n := led.findingEntries(); n != 1200 {
		t.Errorf("ledger holds %d of 1200 findings", n)
	}
}
