package main

// AEGIS approved-with-conditions review of fix-revrec 8bdebde (Oct 7 2026),
// through the real HTTP routes: M3 (a future as_of is refused), L2 (a failed
// scan releases the one-scan lock), L1 (a commission's base and rate are
// recorded in the ledger and served with the finding).

import (
	"encoding/json"
	"net/http"
	"strings"
	"testing"
	"time"
)

// M3 (probe P11 through as_of): ?as_of= in the future made every lapsed
// subscription due before it a "missed renewal". Refused with 422 before
// anything runs; within the stated clock-skew tolerance (60 s) it is
// accepted.
func TestM3_FutureAsOfIsRefusedAndRunsNothing(t *testing.T) {
	det, led := twoLeakDetection(), newMemLedger()
	srv := fixWaveServer(t, det, led)
	future := time.Now().Add(10 * time.Minute).UTC().Format(time.RFC3339)
	code, _, body := scan(t, srv, "?as_of="+future)
	if code != http.StatusUnprocessableEntity || !strings.Contains(body, "in the future") || !strings.Contains(body, "1m0s") {
		t.Fatalf("future as_of = %d: %s", code, body)
	}
	if n := len(det.requests()); n != 0 {
		t.Errorf("a refused scan made %d detection calls", n)
	}
	if n := len(led.snapshot()); n != 0 {
		t.Errorf("a refused scan wrote %d ledger entries", n)
	}
	skewed := time.Now().Add(30 * time.Second).UTC().Format(time.RFC3339)
	if code, _, body := scan(t, srv, "?as_of="+skewed); code != http.StatusOK {
		t.Errorf("as_of within the clock-skew tolerance = %d: %s", code, body)
	}
	past := time.Now().Add(-24 * time.Hour).UTC().Format(time.RFC3339)
	if code, _, body := scan(t, srv, "?as_of="+past); code != http.StatusOK {
		t.Errorf("past as_of = %d: %s", code, body)
	}
}

// L2: a scan that fails — at the ledger, mid-write, or at detection —
// releases the one-scan lock, so the next scan runs (200), not 409.
func TestL2_FailedScanReleasesTheScanLock(t *testing.T) {
	t.Run("ledger write fails midway", func(t *testing.T) {
		det, led := twoLeakDetection(), newMemLedger()
		led.failAt[2] = true
		led.failAt[3], led.failAt[4], led.failAt[5] = true, true, true // every retry of that write fails too
		srv := fixWaveServer(t, det, led)
		if code, _, body := scan(t, srv, ""); code != http.StatusBadGateway {
			t.Fatalf("first scan = %d, want 502: %s", code, body)
		}
		code, sb, body := scan(t, srv, "")
		if code != http.StatusOK {
			t.Fatalf("scan after a failed one = %d (409 = the lock was not released): %s", code, body)
		}
		if fb := recorded(t, srv); len(fb.Findings) != 3 || len(fb.ExcludedScans) != 1 {
			t.Errorf("after recovery: %d findings, excluded %+v (scan %s)", len(fb.Findings), fb.ExcludedScans, sb.ScanID)
		}
	})
	t.Run("detection fails", func(t *testing.T) {
		det, led := twoLeakDetection(), newMemLedger()
		det.rules["discount-misuse-v1"] = func(c string, _ []map[string]any) []map[string]any {
			return []map[string]any{mkFinding("other-client", "discount-misuse-v1", "order", "o0", "")}
		}
		srv := fixWaveServer(t, det, led)
		if code, _, body := scan(t, srv, ""); code != http.StatusBadGateway {
			t.Fatalf("first scan = %d, want 502: %s", code, body)
		}
		delete(det.rules, "discount-misuse-v1")
		if code, _, body := scan(t, srv, ""); code != http.StatusOK {
			t.Fatalf("scan after a failed one = %d: %s", code, body)
		}
	})
	t.Run("refused request", func(t *testing.T) {
		det, led := twoLeakDetection(), newMemLedger()
		srv := fixWaveServer(t, det, led)
		if code, _, _ := scan(t, srv, "?client_id=someone-else"); code != http.StatusUnprocessableEntity {
			t.Fatalf("foreign tenant = %d", code)
		}
		if code, _, body := scan(t, srv, ""); code != http.StatusOK {
			t.Fatalf("scan after a refused one = %d: %s", code, body)
		}
	})
}

// L1: the affiliate commission's base and rate travel from detection-py to
// the ledger (an rr_value_basis event) and back out on the findings view.
func TestL1_CommissionBaseIsRecordedAndServed(t *testing.T) {
	det, led := newFakeDetection(), newMemLedger()
	det.fixtures["/fixtures/orders"] = orders(1)
	det.rules["affiliate-coupon-extension-v1"] = func(c string, items []map[string]any) []map[string]any {
		f := mkFinding(c, "affiliate-coupon-extension-v1", "order", "o0", "12.00")
		f["recoverable_value"] = map[string]any{"amount_usd": "12.00", "classification": "attributed", "confidence": "medium"}
		f["evidence_class"] = "ESTIMATED"
		f["value_basis"] = map[string]any{"base_usd": "120.00", "rate_percent": "10"}
		return []map[string]any{f}
	}
	srv := fixWaveServer(t, det, led)
	if code, _, body := scan(t, srv, ""); code != http.StatusOK {
		t.Fatalf("scan = %d: %s", code, body)
	}
	var basis map[string]any
	for _, e := range led.snapshot() {
		if e["event_type"] == "rr_value_basis" {
			basis = e
		}
	}
	if basis == nil || basis["summary"] != "rrb1 base=120.00 rate=10" ||
		basis["subject_id"] != findingID(tenant, "affiliate-coupon-extension-v1", "order", "o0", "") {
		t.Fatalf("value basis event: %v", basis)
	}
	_, body := do(t, http.MethodGet, srv.URL+"/revenue-recovery/findings", routeTestToken)
	var fb struct {
		Findings []struct {
			ValueBasis *struct {
				BaseUSD     string `json:"base_usd"`
				RatePercent string `json:"rate_percent"`
			} `json:"value_basis"`
		} `json:"findings"`
	}
	if err := json.Unmarshal([]byte(body), &fb); err != nil || len(fb.Findings) != 1 || fb.Findings[0].ValueBasis == nil ||
		fb.Findings[0].ValueBasis.BaseUSD != "120.00" || fb.Findings[0].ValueBasis.RatePercent != "10" {
		t.Fatalf("findings view: %v %s", err, body)
	}
}

// M2 at the boundary: a detection-py that still sends ESTIMATED evidence
// labeled observed/high (8bdebde's renewal) fails the scan with nothing
// written.
func TestM2_OverclaimingFindingFailsTheScanWithNothingWritten(t *testing.T) {
	det, led := newFakeDetection(), newMemLedger()
	det.fixtures["/fixtures/subscriptions"] = `[{"subscription_id":"s1"}]`
	det.rules["renewal-never-triggered-v1"] = func(c string, _ []map[string]any) []map[string]any {
		f := mkFinding(c, "renewal-never-triggered-v1", "subscription", "s1", "39.00") // observed/high
		f["evidence_class"] = "ESTIMATED"
		return []map[string]any{f}
	}
	srv := fixWaveServer(t, det, led)
	if code, _, body := scan(t, srv, ""); code != http.StatusBadGateway || !strings.Contains(body, "fails the scan contract") {
		t.Fatalf("over-claiming finding = %d: %s", code, body)
	}
	if n := len(led.snapshot()); n != 0 {
		t.Errorf("%d ledger entries written", n)
	}
}
